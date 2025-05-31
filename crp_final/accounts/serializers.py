from rest_framework import serializers
from accounts.models import User
from django.utils.encoding import smart_str, force_bytes, DjangoUnicodeDecodeError
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from accounts.utils import Util

class UserRegistrationSerializer(serializers.ModelSerializer):
    """
    Serializer for registering a new user with password confirmation.
    """
    password2 = serializers.CharField(style={'input_type': 'password'}, write_only=True)

    class Meta:
        model = User
        fields = ['email', 'name', 'password', 'password2', 'tc']
        extra_kwargs = {
            'password': {'write_only': True}
        }

    def validate(self, attrs):
        """
        Check that the two password entries match.
        """
        if attrs.get('password') != attrs.get('password2'):
            raise serializers.ValidationError("Password and Confirm Password do not match.")
        return attrs

    def create(self, validated_data):
        """
        Create user with validated data, excluding password2.
        """
        validated_data.pop('password2', None)
        return User.objects.create_user(**validated_data)


class UserLoginSerializer(serializers.ModelSerializer):
    """
    Serializer for user login request.
    """
    email = serializers.EmailField(max_length=255)

    class Meta:
        model = User
        fields = ['email', 'password']


class UserProfileSerializer(serializers.ModelSerializer):
    """
    Serializer for returning user profile information, including permission-related fields.
    """
    class Meta:
        model = User
        fields = [
            'id', 'email', 'name', 'tc',
            'is_active', 'is_staff', 'is_superuser',
            'groups', 'user_permissions', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'is_active', 'is_admin', 'is_staff', 'is_superuser',
            'groups', 'user_permissions', 'created_at', 'updated_at'
        ]


class UserChangePasswordSerializer(serializers.Serializer):
    """
    Serializer for allowing users to change their password.
    """
    password = serializers.CharField(max_length=255, style={'input_type': 'password'}, write_only=True)
    password2 = serializers.CharField(max_length=255, style={'input_type': 'password'}, write_only=True)

    class Meta:
        fields = ['password', 'password2']

    def validate(self, attrs):
        """
        Check that the two password entries match and update the password.
        """
        password = attrs.get('password')
        password2 = attrs.get('password2')
        user = self.context.get('user')

        if password != password2:
            raise serializers.ValidationError("Password and Confirm Password do not match.")

        user.set_password(password)
        user.save()
        return attrs


class SendPasswordResetEmailSerializer(serializers.Serializer):
    """
    Serializer for requesting a password reset email.
    """
    email = serializers.EmailField(max_length=255)

    class Meta:
        fields = ['email']

    def validate(self, attrs):
        """
        Validate that the email exists and send reset link if valid.
        """
        email = attrs.get('email')
        if User.objects.filter(email=email).exists():
            user = User.objects.get(email=email)
            uid = urlsafe_base64_encode(force_bytes(user.id))
            token = PasswordResetTokenGenerator().make_token(user)
            reset_link = f'http://localhost:3000/api/user/reset/{uid}/{token}'
            body = f'Click the following link to reset your password: {reset_link}'
            data = {
                'subject': 'Reset Your Password',
                'body': body,
                'to_email': user.email
            }
            # Uncomment the following line to actually send the email
            # Util.send_email(data)
            return attrs
        else:
            raise serializers.ValidationError('You are not a registered user.')


class UserPasswordResetSerializer(serializers.Serializer):
    """
    Serializer for resetting the user's password using token and UID.
    """
    password = serializers.CharField(max_length=255, style={'input_type': 'password'}, write_only=True)
    password2 = serializers.CharField(max_length=255, style={'input_type': 'password'}, write_only=True)

    class Meta:
        fields = ['password', 'password2']

    def validate(self, attrs):
        """
        Validate token, UID, and reset password if valid.
        """
        try:
            password = attrs.get('password')
            password2 = attrs.get('password2')
            uid = self.context.get('uid')
            token = self.context.get('token')

            if password != password2:
                raise serializers.ValidationError("Password and Confirm Password do not match.")

            user_id = smart_str(urlsafe_base64_decode(uid))
            user = User.objects.get(id=user_id)

            if not PasswordResetTokenGenerator().check_token(user, token):
                raise serializers.ValidationError('The reset token is invalid or has expired.')

            user.set_password(password)
            user.save()
            return attrs

        except DjangoUnicodeDecodeError:
            raise serializers.ValidationError('The reset token is invalid or has expired.')
