from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from django.contrib.auth import authenticate
from rest_framework_simplejwt.tokens import RefreshToken
# Import extend_schema
from drf_spectacular.utils import extend_schema

from accounts.serializers import (
    UserRegistrationSerializer, UserLoginSerializer, UserProfileSerializer,
    UserChangePasswordSerializer, SendPasswordResetEmailSerializer, UserPasswordResetSerializer
)
from accounts.renderers import UserRenderer

# Helper function to generate tokens
def get_tokens_for_user(user):
    """
    Generate refresh and access tokens manually for the given user.
    """
    refresh = RefreshToken.for_user(user)
    return {
        'refresh': str(refresh),
        'access': str(refresh.access_token),
    }

class UserRegistrationView(APIView):
    """
    API endpoint for user registration.
    """
    renderer_classes = [UserRenderer]

    # Add extend_schema here
    @extend_schema(
        request=UserRegistrationSerializer,
        responses={201: UserRegistrationSerializer} # Optional: Define response schema too
    )
    def post(self, request, format=None):
        serializer = UserRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        token = get_tokens_for_user(user)
        return Response({'token': token, 'msg': 'Registration successful.'}, status=status.HTTP_201_CREATED)

class UserLoginView(APIView):
    """
    API endpoint for user login.
    """
    renderer_classes = [UserRenderer]

    # Add extend_schema here
    @extend_schema(
        request=UserLoginSerializer,
        responses={200: None, 404: None} # Indicate response bodies or lack thereof
    )
    def post(self, request, format=None):
        serializer = UserLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data.get('email')
        password = serializer.validated_data.get('password')
        user = authenticate(username=email, password=password)

        if user is not None:
            token = get_tokens_for_user(user)
            return Response({'token': token, 'msg': 'Login successful.'}, status=status.HTTP_200_OK)
        else:
            return Response(
                {'errors': {'non_field_errors': ['Email or Password is not valid.']}},
                status=status.HTTP_404_NOT_FOUND
            )

class UserProfileView(APIView):
    """
    API endpoint to retrieve the authenticated user's profile.
    """
    renderer_classes = [UserRenderer]
    permission_classes = [IsAuthenticated]

    # GET requests usually infer response from serializer, but explicit is good
    @extend_schema(
        responses=UserProfileSerializer
    )
    def get(self, request, format=None):
        serializer = UserProfileSerializer(request.user)
        return Response(serializer.data, status=status.HTTP_200_OK)

class UserChangePasswordView(APIView):
    """
    API endpoint for allowing an authenticated user to change their password.
    """
    renderer_classes = [UserRenderer]
    permission_classes = [IsAuthenticated]

    # Add extend_schema here
    @extend_schema(
        request=UserChangePasswordSerializer,
        responses={200: None, 400: None} # Indicate success/failure
    )
    def post(self, request, format=None):
        serializer = UserChangePasswordSerializer(data=request.data, context={'user': request.user})
        serializer.is_valid(raise_exception=True)
        return Response({'msg': 'Password changed successfully.'}, status=status.HTTP_200_OK)

class SendPasswordResetEmailView(APIView):
    """
    API endpoint to send a password reset link to a user's email.
    """
    renderer_classes = [UserRenderer]

    # Add extend_schema here
    @extend_schema(
        request=SendPasswordResetEmailSerializer,
        responses={200: None, 400: None}
    )
    def post(self, request, format=None):
        serializer = SendPasswordResetEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response({'msg': 'Password reset link sent. Please check your email.'}, status=status.HTTP_200_OK)

class UserPasswordResetView(APIView):
    """
    API endpoint to reset a user's password using UID and token from email.
    """
    renderer_classes = [UserRenderer]

    # Add extend_schema here
    # Note: uid and token are path parameters, handled by URL conf, not request body
    @extend_schema(
        request=UserPasswordResetSerializer,
        responses={200: None, 400: None}
    )
    def post(self, request, uid, token, format=None):
        # uid and token are automatically documented as path parameters
        # if defined correctly in your urls.py
        serializer = UserPasswordResetSerializer(data=request.data, context={'uid': uid, 'token': token})
        serializer.is_valid(raise_exception=True)
        return Response({'msg': 'Password reset successfully.'}, status=status.HTTP_200_OK)