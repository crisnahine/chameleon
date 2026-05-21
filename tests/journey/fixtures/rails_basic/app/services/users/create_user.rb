module Users
  class CreateUser
    def initialize(params)
      @params = params
    end

    def call
      User.create!(@params)
    rescue ActiveRecord::RecordInvalid => e
      Result.failure(e.message)
    end
  end
end
